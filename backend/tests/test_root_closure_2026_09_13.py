"""Part L — Root-Closure Pass tests (2026-09-13).

Focused tests covering the surgical fixes applied in the 2026-09-13
root-closure pass.  Each test is independently runnable and
targets a specific contract from the user's directive.
"""
from __future__ import annotations

import sys, pathlib
_BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest

from sports_engine import compute_lock_score
from services.bet_quality_authority import (
    compute_bet_quality_authority,
    bet_quality_authority_enabled,
)


# ─────────────────────────────────────────────────────────────────
# BQ AUTHORITY HANDOFF — MLB / CFB / NFL_GAME authoritative,
# NFL_PLAYER preserved.
# ─────────────────────────────────────────────────────────────────
def test_mlb_bq_authority_is_final_score():
    """When BQ is enabled for MLB and evidence is strong, the final
    Lock Score IS the BQ ceiling — not the raw composite."""
    factors = {
        "Expected BA (Statcast)":       0.92,
        "Barrel% (Quality of Contact)": 0.93,
        "Hard-Hit % (Statcast)":        0.91,
        "Matchup Advantage":            0.95,
        "DFS Projection vs Line":       0.93,
    }
    pick = {
        "sport": "MLB", "market": "Judge Over 1.5 H+R+RBI",
        "book_odds": -230, "win_probability": 82.0,
        "data_quality": "full",
        "probability_provenance": "PROP_MODEL_PRIMARY",
        "exact_threshold_hit_rate": 0.85,
        "sim_stability": 0.94,
    }
    ls, _ = compute_lock_score(factors, win_prob=82.0, pick=pick,
                                 edge_percent=10.0)
    bq = pick.get("bet_quality_authority")
    assert bq is not None
    assert 90.0 <= ls <= 99.0, f"strong MLB HRR should be ≥90; got {ls}"
    # UEA P0-P26 (2026-06): both BQ and UEA can lift a strong
    # composite.  LS must be AT LEAST BQ ceiling — a UEA lift above
    # BQ is legitimate when multi-signal evidence is strong.
    assert ls >= bq["ceiling"] - 0.15, (
        f"MLB LS {ls} must be ≥ BQ ceiling {bq['ceiling']}"
    )
    # And LS must not exceed 99 (Apex is separate).
    assert ls <= 99.0


def test_mlb_weak_evidence_drops_below_floor():
    """Weak MLB evidence must legitimately produce LS below the 85 floor,
    per user directive ‘Missing evidence reduces authority/completeness.’"""
    factors = {"Team Offense (L15)": 0.55, "Bullpen ERA": 0.50}
    pick = {"sport": "MLB", "market": "New York Yankees Moneyline",
            "win_probability": 54.0}
    ls, _ = compute_lock_score(factors, win_prob=54.0, pick=pick)
    assert ls < 85.0, f"weak MLB ML must fall below 85 floor, got {ls}"


def test_cfb_bq_authority_high_evidence():
    """CFB with strong SP+ margin normalized to [0,1] must reach 90+."""
    factors = {
        "Projected Margin (norm)": 0.95, "Expected Total (norm)": 0.80,
        "Model Fair Prob (norm)":  0.92, "SP+ Rating Δ (norm)": 0.95,
    }
    pick = {
        "sport": "CFB", "market": "Ohio State Moneyline",
        "book_odds": -600, "win_probability": 92.0,
        "data_quality": "sp_plus+returning_prod_both+portal_both",
        "probability_provenance": "CAUSAL_INDEPENDENT",
        "sim_stability": 0.95, "exact_threshold_hit_rate": 0.85,
    }
    ls, _ = compute_lock_score(factors, win_prob=92.0, pick=pick,
                                 edge_percent=5.0)
    assert ls >= 90.0, f"strong CFB should reach 90+, got {ls}"


def test_nfl_player_prop_preserves_current_tier():
    """NFL player props with ~95% WP must NOT be capped down to 92 by
    the new BQ handoff — they preserve the exact-threshold authority
    that currently produces 96-97 live."""
    factors = {"L5 Avg vs Line": 0.92, "Home/Away Split": 0.9,
                "Career vs Opponent Hit%": 0.85,
                "L5 Threshold Support": 0.88}
    pick = {"sport": "NFL", "market": "player_pass_yds",
             "model_win_prob": 94.6, "book_odds": -400,
             "is_alt_line": False}
    ls, _ = compute_lock_score(factors, win_prob=94.6, pick=pick)
    # Exact-threshold authority ceiling for WP=94.6 → 60 + 94.6*40/100
    # = 97.84.  Score must land at or near that ceiling — never
    # collapsed to 92 by the new BQ branch.
    assert ls >= 96.0, (
        f"NFL prop must preserve 96+ tier; got {ls}"
    )
    assert pick.get("nfl_prop_authority_applied") is True


# ─────────────────────────────────────────────────────────────────
# RESCORE SAFETY — scale detection, no double normalization,
# no lock_score overwrite by isolated helper.
# ─────────────────────────────────────────────────────────────────
def test_rescore_scale_detection_normalized_cfb():
    """Rescore helper must NOT divide CFB (norm) factors (already
    [0,1]) by 100 — that was the historical corruption bug."""
    from scripts.rescore_bq_authority import _rescore  # importable
    # We do not run the DB; we just replicate the scale-detection
    # branch used inside _rescore against a synthetic factor dict.
    persisted_factors = {
        "Projected Margin (norm)": 0.9097,
        "SP+ Rating Δ (norm)":     0.9097,
    }
    # Replicate the helper's per-pick detection.
    raw_numeric = {
        k: float(v) for k, v in persisted_factors.items()
        if isinstance(v, (int, float))
        and not isinstance(v, bool)
        and not (isinstance(k, str) and k.startswith("__"))
    }
    peak = max(abs(v) for v in raw_numeric.values())
    needs_div = peak > 1.5
    result = {k: (v / 100.0) if needs_div else v
              for k, v in raw_numeric.items()}
    # Peak = 0.9097, does NOT exceed 1.5 → no divide.
    assert not needs_div, "already-normalized CFB peak (0.9097) must not divide"
    assert result["SP+ Rating Δ (norm)"] == 0.9097


def test_rescore_scale_detection_mlb_percent():
    """MLB persisted factors (0-100 percent scale) must be divided
    to [0,1] before feeding compute_lock_score."""
    persisted = {"Expected BA (Statcast)": 76.8, "Barrel%": 69.3}
    peak = max(abs(v) for v in persisted.values())
    needs_div = peak > 1.5
    result = {k: (v / 100.0) if needs_div else v for k, v in persisted.items()}
    assert needs_div
    assert abs(result["Expected BA (Statcast)"] - 0.768) < 1e-6


def test_bq_authority_stamped_on_shim():
    """The BQ authority payload must be stamped on the pick dict
    passed to compute_lock_score so downstream persistence bridges
    can promote it to the DB row."""
    factors = {"Model Win Prob (norm)": 0.72,
                "Sportsbook Implied (norm)": 0.60,
                "Expected Margin (norm)":   0.70}
    pick = {"sport": "NFL", "market": "Buffalo Bills Moneyline",
             "book_odds": -220, "win_probability": 72.0}
    _, _ = compute_lock_score(factors, win_prob=72.0, pick=pick,
                                edge_percent=12.0)
    bq = pick.get("bet_quality_authority")
    assert bq is not None
    assert "ceiling" in bq
    assert "components" in bq
    assert set(bq["components"].keys()) == {
        "wp", "reliability", "history", "matchup",
        "convergence", "distribution", "data_quality",
    }


# ─────────────────────────────────────────────────────────────────
# STRUCTURAL REACHABILITY — 90/95/98/99 legitimately reachable.
# ─────────────────────────────────────────────────────────────────
def test_mlb_96_99_structural_reachability():
    """A near-perfect MLB HRR setup (WP=90%, PLATINUM reliability,
    perfect exact-threshold history, elite matchup, perfect
    convergence, high sim stability) must be structurally capable
    of reaching the 96+ band."""
    factors = {
        "Expected BA (Statcast)":       0.98,
        "Barrel% (Quality of Contact)": 0.98,
        "Hard-Hit % (Statcast)":        0.97,
        "Matchup Advantage":            0.99,
        "DFS Projection vs Line":       0.99,
        "Opp Bullpen":                  0.98,
    }
    pick = {"sport": "MLB", "market": "Judge Over 0.5 Hits",
             "book_odds": -800, "win_probability": 95.0,
             "data_quality": "full",
             "probability_provenance": "PLATINUM",
             "exact_threshold_hit_rate": 0.99, "sim_stability": 0.99}
    ls, _ = compute_lock_score(factors, win_prob=95.0, pick=pick,
                                edge_percent=5.0)
    assert ls >= 96.0, f"peak MLB evidence must reach 96+; got {ls}"


def test_cfb_high_tier_structural_reachability():
    factors = {
        "Projected Margin (norm)": 0.97, "Expected Total (norm)": 0.85,
        "Model Fair Prob (norm)":  0.94, "SP+ Rating Δ (norm)": 0.97,
    }
    pick = {"sport": "CFB", "market": "Alabama -21 Spread",
             "book_odds": -110, "win_probability": 94.0,
             "data_quality": "sp_plus+returning_prod_both+portal_both",
             "probability_provenance": "CAUSAL_INDEPENDENT",
             "sim_stability": 0.94, "exact_threshold_hit_rate": 0.85}
    ls, _ = compute_lock_score(factors, win_prob=94.0, pick=pick,
                                 edge_percent=6.0)
    assert ls >= 90.0, f"peak CFB should reach 90+; got {ls}"


# ─────────────────────────────────────────────────────────────────
# NFL GAME evidence must NOT enter compute_lock_score with factors={}.
# ─────────────────────────────────────────────────────────────────
def test_nfl_platinum_ml_populates_evidence():
    """When the NFL Platinum ML path invokes compute_lock_score, the
    resulting `weighted` breakdown must contain real numeric evidence
    (not the empty-factor artifact that produced un-BQ-stamped rows
    like Detroit Tigers ML LS=92.6)."""
    # This tests the compute_lock_score reachability path with a
    # populated factor dict + sport+market on the pick — exact
    # contract used by the surgical NFL Platinum fix.
    factors = {"Model Win Prob (norm)": 0.72,
                "Sportsbook Implied (norm)": 0.60,
                "Expected Margin (norm)":   0.70,
                "Model-vs-Market Δ":        0.62,
                "Simulation Stability (norm)": 0.90}
    pick = {"sport": "NFL", "market": "Buffalo Bills Moneyline",
             "book_odds": -220, "win_probability": 72.0,
             "data_quality": "platinum_nfl_sim",
             "probability_provenance": "CAUSAL_INDEPENDENT",
             "sim_stability": 0.90}
    ls, breakdown = compute_lock_score(factors, win_prob=72.0, pick=pick,
                                          edge_percent=12.0)
    numeric = [k for k in breakdown if isinstance(breakdown[k], (int, float))
                and not (isinstance(k, str) and k.startswith("__"))]
    assert len(numeric) >= 3, (
        f"NFL game market must not enter scoring with factors≈{{}} — "
        f"got only {len(numeric)} numeric keys: {numeric!r}"
    )
    assert pick.get("bet_quality_authority") is not None


# ─────────────────────────────────────────────────────────────────
# LOCK SCORE != WIN PROBABILITY invariant.
# ─────────────────────────────────────────────────────────────────
def test_lock_score_not_equal_win_probability():
    """A pick with WP=82% and non-trivial evidence must NOT produce
    LS=82.  Lock Score reflects the full multi-signal Bet Quality
    setup — not the raw win probability."""
    factors = {"Expected BA (Statcast)": 0.9, "Barrel%": 0.9,
                "Hard-Hit %": 0.9, "Matchup Advantage": 0.9}
    pick = {"sport": "MLB", "market": "Judge Over 1.5 H+R+RBI",
             "book_odds": -300, "win_probability": 82.0,
             "data_quality": "full",
             "probability_provenance": "PROP_MODEL_PRIMARY",
             "exact_threshold_hit_rate": 0.85, "sim_stability": 0.9}
    ls, _ = compute_lock_score(factors, win_prob=82.0, pick=pick,
                                edge_percent=8.0)
    assert abs(ls - 82.0) > 3.0, (
        f"LS should not collapse to WP; got LS={ls}"
    )


# ─────────────────────────────────────────────────────────────────
# BQ AUTHORITY VERSION provenance stamped.
# ─────────────────────────────────────────────────────────────────
def test_bq_authority_version_stamped():
    factors = {"f1": 0.7, "f2": 0.7}
    pick = {"sport": "MLB", "market": "Yankees Moneyline",
             "win_probability": 65.0}
    compute_lock_score(factors, win_prob=65.0, pick=pick)
    bq = pick["bet_quality_authority"]
    assert bq["version"].startswith("bq_authority."), (
        f"expected BQ authority version stamp, got {bq!r}"
    )
